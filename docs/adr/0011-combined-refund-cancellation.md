# ADR 0011: Combined refund and period-end cancellation

Status: Accepted locally on 2026-11-12. The design was accepted on 2026-11-06.
This is not acceptance of all M3, hybrid-retrieval implementation, or a paid
evaluation.

Problem: a customer can ask for a duplicate refund and for the subscription to
end at period end in the same ticket. `intent_gate` in
`src/resolveai/domain/cancellation.py` treats any refund phrase together with
any cancellation phrase as `combined_intent`. The server then hands off with
no proposal and no simulator operation. Separate refund tickets and separate
cancellation tickets pass their own tests. Those tests do not show what this
ticket does when one write succeeds and the other blocks.

Alternatives: keep the combined stop; wrap both writes in one simulator
transaction and roll both back on either failure; reuse one action id and one
approval for both effects; let the model decide which effect to skip; undo a
confirmed refund because the cancellation did not schedule.

Choice: remove the combined stop only for this ordinary billing family. Each
half calls the gateway it already uses. The refund keeps action id
`refund:{payment_id}`. The cancellation keeps action id
`cancel_at_period_end:{subscription_id}:{effective_at}`. Each proposal has its
own revision, payload hash, approval, ledger row, and recovery job. Settling
one does not approve, settle, reverse, or resend the other. A confirmed
receipt stays. An uncertain half remains on its original action id and is
queried again. A half that was not executed does not receive a new action id.

## What already exists

Refund creation, duplicate rules, and query-only recovery after admission live
in `src/resolveai/services/proposals.py`, `src/resolveai/services/recovery.py`,
and `src/resolveai/worker/`. Period-end creation, the choice gate, and command
binding live in `src/resolveai/domain/cancellation.py` and
`src/resolveai/services/cancellation.py`. ADR 0009 accepted that cancellation
path locally on 2026-11-02. ADR 0010, accepted locally on 2026-11-06, refuses
both financial proposals when the ticket asks about an incident or
compensation. That refusal stays.

The proposal unique key is `(tenant_id, action_id, revision)`. Two open
proposals with different action ids can already be stored. The workbench in
`apps/web/src/App.tsx` renders one non-superseded proposal. The ticket status
is one summary value. A customer reply is one draft.

The current single-action callbacks cannot be reused unchanged for this
family. Cancellation confirmation in `src/resolveai/services/recovery.py` and
the unknown-refund path in `src/resolveai/services/execution.py` each call
`upsert_reply_draft` and move the shared run and ticket into reply review.
`upsert_reply_draft` in `src/resolveai/services/replies.py` can return an
unsent draft with the same body without checking the run or the input
revision. The ordinary send path checks the draft revision, body hash,
recipient, and reply ledger. It does not recheck a second action. A half that
is waiting for a choice, or is ineligible, has no ledger row at all.

The execute route commits a queued recovery job. That commit is not a
simulator effect and not a receipt. Admission, the business write, and the
later confirming query are separate stages. After admission, recovery queries
the original action id.

## Family

The only new family is one eligible duplicate payment and one future
unscheduled subscription on the ticket's bound account. The customer text
asks for the refund and for period-end cancellation. Immediate termination is
still not scheduled. An incident or compensation phrase still produces no
refund proposal and no cancellation proposal.

Each half is judged by its own rule:

- The refund half proposes only when the existing duplicate rule says the
  payment is eligible. Otherwise the run explains why there is no refund
  proposal. It does not invent an amount.
- The cancellation half proposes only when the existing period-end rule,
  including the stored-choice gate, says one subscription can be scheduled.
  An immediate phrase with no later period-end choice leaves that half
  `waiting`. It does not block an eligible refund.
- If both halves are ineligible, the ticket stays `open` with both
  explanations. No operation is created, and the ticket is not automatically
  handed off.

Partial eligibility is not partial execution. A proposal exists only for a
half the server is willing to submit for approval.

Support may investigate and record a subscription choice. Support cannot
approve either proposal. The approver approves each proposal with that
proposal's revision and payload hash. One grant does not approve the other
half. Revoking or expiring one grant before enqueue leaves the other grant
as stored. There is no new worker command and no shared rollback.

## Half outcomes

Each half has one current outcome. The server first chooses the current
attempt, then classifies that attempt. The first matching class wins.

The current attempt is chosen under the ticket lock:

1. If any job for this half's action id has been admitted, that admitted job
   is current. Older cancelled or blocked jobs for the same action id are
   history. The class is then `confirmed` or `uncertain`.
2. Otherwise the current attempt is the newest authorized one: the
   `combined_half` version with the greatest `outcome_version` for this input
   revision and half. A blocked or cancelled job with a lower version is not
   current.
3. A later attempt may be authorized only from `open`, `not_dispatched`,
   `waiting`, or `not_executed`. `confirmed` and `uncertain` do not authorize
   a new action id or a new dispatch. A `not_dispatched` retry uses the same
   proposal and the same action id only when the input revision is unchanged,
   the exact grant is still live, the actor may execute, and fresh resource,
   policy, and precondition checks pass. No admitted command may exist. The
   class itself is not permission. New customer or operator input supersedes
   the unsent proposal and cancels its pre-admission job. The old approval
   does not authorize the changed input. A newly eligible revision needs its
   own approval. The retry inserts a new outcome version and does not rewrite
   the blocked row. An approved payload or grant is not edited to absorb a
   stale version or an elapsed period.

| Order | Class | When it applies to the current attempt | What the customer text may say |
| --- | --- | --- | --- |
| 1 | `confirmed` | The ledger for this action id is confirmed and the receipt matches that action. A simulator row found by a later query is this class, including a schedule that committed before the response was lost. | Quote that receipt. |
| 2 | `uncertain` | The current job is admitted and the ledger is not confirmed. This includes `unknown`, `delivery_uncertain`, an unavailable query, and a commit in the simulator followed by a lost response. | Say this action id is not confirmed. Do not say the subscription was not scheduled, and do not say the refund was not paid. |
| 3 | `in_flight` | The current job is queued or leased, and `admitted_at` is null. | No customer draft yet. |
| 4 | `not_dispatched` | The current job is `blocked`, `admitted_at` is null, and `block_reason` is the transient read failure `unavailable`. The proposal can still be approved. Definite stops such as `version_conflict`, `period_elapsed`, `already_scheduled`, and `authorization_stale` are not this class. | Say this attempt was not sent because the read was unavailable. Do not say the customer is ineligible, and do not say the business effect is absent. |
| 5 | `open` | A live proposal is open or approved, and no job has been enqueued for the current attempt. | No customer draft yet. |
| 6 | `waiting` | The half needs a subscription choice or a later period-end confirmation. No proposal and no ledger. | Say a choice is still required. Do not say a cancellation action was executed or refused. |
| 7 | `not_executed` | The half is ineligible, the approver rejected it, the grant expired or was revoked before admission (including after enqueue), the proposal was superseded before admission, a pre-admission job was cancelled by new input, or this attempt stopped for `version_conflict`, `period_elapsed`, `already_scheduled`, or `authorization_stale`. | Say this action was not executed, and give that reason. Do not relabel those reasons as an unavailable read. For cancellation, that is a statement about this action id, not a claim that every subscription on the account is unscheduled. |

`uncertain` keeps the original action id and stays on query-only recovery.
It does not authorize a new proposal, a new dispatch, or a compensating
refund. `not_dispatched` means this attempt did not reach admission. It is
not an eligibility decision. `not_executed` is the only class that supports a
definite no-execution sentence.

## Durable binding

A waiting or ineligible half has no proposal and no ledger, so the run
observation is not enough to bind a later reply. One physical row also cannot
be both immutable and rewritten as the same input moves from `open` to
`in_flight` to `not_dispatched` or `confirmed`. This slice adds versioned
append-only rows. It does not add an action type or a third financial command,
and it does not keep a mutable projection.

`app.combined_half` inserts a new row for every outcome. Rows are never
updated or deleted by `resolveai_api` or `resolveai_worker`. The unique key
is `(tenant_id, ticket_id, input_revision, half, outcome_version)`. `half` is
`refund` or `cancel_at_period_end`. `outcome_version` starts at 1 for that
input revision and half, and the next insert uses the greatest version plus
one. The ticket lock makes that choice serial. The current outcome is the
row with the greatest `outcome_version`, then the greatest `id`.

| Column | Role |
| --- | --- |
| `id` | Immutable identity of this outcome version. It is never null. |
| `action_id` | Null for `waiting`, for `not_executed` without a selected target, and for a `not_dispatched` attempt that has no stable target. Otherwise the existing refund or cancellation action id. |
| `proposal_id`, `recovery_job_id` | Null when the outcome has no financial row. `waiting` and ineligible `not_executed` are identified by `id` alone. |
| `outcome_code` | One class from the table above. |
| `reason_code` | Required for `waiting`, `not_executed`, and `not_dispatched`. Empty for the other classes. |
| `prior_outcome_id` | The previous version of this half, or null on the first version. |

The same proposal revision and the same job generation can still produce
several versions. Enqueue, a pre-admission block, and a later renewal are
different `id` values even when the proposal revision does not change. The
reply binds to those ids, not to the revision or generation alone.

The current binding is the current version of each half at the ticket's
latest input revision. Older versions and older input revisions stay. A new
input revision inserts new first versions. A `confirmed` or `uncertain`
action id is copied forward by inserting a new row with the same action id
and `prior_outcome_id` pointing at the previous version. That copy does not
open another proposal and does not authorize a new dispatch.

`app.combined_reply` binds one reply draft to one run, one input revision,
and the two immutable outcome ids. Ordinary single-purpose drafts do not get
a row. Their send path stays as it is.

Downgrade of this migration refuses while any combined row exists. It does
not delete refund, cancellation, approval, or recovery history. The history
trigger rejects runtime updates and deletes. The tenant foreign keys match
the ticket, and the reply foreign keys match `(tenant_id, combined_half.id)`.

## One reduction

Single-action confirmation, rejection, and unknown paths must not call
`upsert_reply_draft` for a ticket that has a current combined binding.
They update their own ledger and job, then insert a new `combined_half`
version. One function, `reduce_combined_reply`, is the only writer of the
combined draft. It runs under the ticket lock after a new version is inserted
and after new input is recorded.

Reply text is a fixed template, refund half first. It includes a section
only for `confirmed`, `uncertain`, `waiting`, `not_dispatched`, and
`not_executed`. It quotes ledger receipts for `confirmed`. For `uncertain`
it says the action is unconfirmed and still being checked. For
`not_dispatched` it says that attempt was not sent because the read was
unavailable. It never calls that failure ineligibility, and it never claims
a missing schedule or a missing refund from an uncertain query.

The first matching row decides the shared run, the ticket summary, and
whether a draft is allowed:

| Order | Half condition | Run | Ticket | Draft |
| --- | --- | --- | --- | --- |
| 1 | Any half is `open` or `in_flight` | `awaiting_approval` | `awaiting_approval` | None. Both halves stay visible, including one that is already `confirmed`. |
| 2 | Any half is `waiting`, and none is `confirmed` or `uncertain` | `awaiting_customer` | `open` | None. The wait is the choice note, not a customer reply. |
| 3 | Any half is `uncertain` or `confirmed`, and every half is `confirmed`, `uncertain`, `waiting`, `not_dispatched`, or `not_executed` | `awaiting_reply_review` | `awaiting_reply` | One current draft bound to the two current outcome ids. |
| 4 | Every half is `not_executed` or `not_dispatched` | `completed` | `open` | None. The explanations stay on the ticket. This is not `handed_off`. A person may hand off later. |

`reply_sent` means a human sent the current combined draft. It does not mean
both halves are finished. The workbench keeps showing a sibling that is
still `uncertain`, `waiting`, `not_dispatched`, or `open`. When a newer outcome
version becomes current, the ticket summary is computed again from the table
above. The sent draft stays sent and visible as history.

Approval expiry or revocation before enqueue moves that half to
`not_executed` and leaves the other grant untouched. Rejection does the same.
Expiry after admission does not cancel the admitted command and does not
create a new action id. The half stays `uncertain` or becomes `confirmed`
only from the original query.

## Reply lifecycle

`reduce_combined_reply` creates a draft only when the table allows one. The
draft's `combined_reply` row stores the run id, the input revision, and both
outcome ids. The same unsent draft is reused only when the body hash and
both ids match. The same words with a different outcome id, a different run,
or a different input revision are a new draft. The previous unsent draft is
superseded. A sent draft is never reused.

Rejecting the current combined draft marks that draft rejected and allows
the reducer to write the next revision for the same binding. Rejecting a
superseded or otherwise historical combined draft does not change the current
ticket, the current run, or the current draft. Sending a sent draft again
returns the same reply receipt.

Before a send receipt is written, the server rechecks the binding under this
lock order: the ticket, then the current refund half, then the current
cancellation half, then the reply draft, then the reply ledger. That ticket
lock is the same first lock the worker already takes in
`_fence` and `_lock_recovery_graph`. The send does not lock a recovery job
before the ticket. If either current outcome id or the input revision has
changed, the send returns stale and writes no receipt.

## Stages and new input

| Stage | What is true | New customer or operator input |
| --- | --- | --- |
| Before enqueue | The half is `waiting`, `open`, or `not_executed`. | Supersede an unsent proposal. No job exists. |
| Enqueue | The execute commit has inserted a queued job and a reserved ledger. There is no simulator effect and no receipt. | Cancel that pre-admission job and supersede the proposal. The half becomes `not_executed` for this attempt. |
| Pre-admission | The job is queued or leased and `admitted_at` is null. | Same cancel as enqueue. No simulator call is authorized afterward. |
| Blocked before admission | The current job is `blocked` and `admitted_at` is null. A transient read exhaustion is `not_dispatched`. A definite stale, elapsed, already-scheduled, or authorization stop is `not_executed` with that reason. | New input supersedes the unsent proposal, cancels the pre-admission job, and invalidates the unsent draft. The old approval does not execute the new input. A same-proposal retry is a separate step and requires unchanged input, a live exact grant, execution authority, fresh precondition checks, and no admitted command. |
| Admitted | `admitted_at` is set. The simulator may or may not have the effect. | Do not cancel the command. Do not open a new proposal or a new action id for this half, even if the old proposal is marked superseded. Recovery stays query-only. |
| Simulator effect | The business row may exist while the ledger is still unknown. | Same fence as admitted. The effect is not confirmation and not evidence of absence. |
| Independent confirmation | A later query matches the existing receipt rule. The ledger is `confirmed`. | The receipt stays. No second proposal for that action id. |

The next investigation may open a new proposal only when the current class is
`waiting` or `not_executed` and the half has no admitted job. That new
proposal needs its own approval. A `not_dispatched` half does not get a new
action id. The same proposal may be enqueued again only under the unchanged
input and live-grant rule above. A `confirmed` or `uncertain` action id is
not proposed again. An older blocked or cancelled job does not outrank the
current version.

Workers resume each job independently. Requeue remains the existing approver
action for one `delivery_uncertain` job. It requeues that job only and does
not dispatch the sibling. Two workers may finish in either order. Each holds
the ticket lock before it inserts a half version or calls the reducer, so
the draft is built from both current versions.

## Acceptance matrix

These are planned checks for a later implementation review. They are not
measured results. Each checked case records both simulator effects, both
half classes, and the current reply binding.

| Case | Refund half | Cancellation half | Current reply |
| --- | --- | --- | --- |
| Both halves eligible, neither enqueued | `open` | `open` | None |
| Both jobs queued, neither admitted | `in_flight` | `in_flight` | None |
| Refund confirmed; cancellation proposal still open | `confirmed` | `open` | None; both halves visible |
| Cancellation waiting for a choice; refund not proposed | `not_executed` or `open` by its own rule | `waiting` | None |
| One grant expired or revoked before enqueue; the other still open | The expired half is `not_executed` | The live half stays `open` | None |
| Approver rejects one proposal; the other is confirmed | The rejected half is `not_executed` | `confirmed` | Names the receipt and says the rejected action was not executed |
| Refund confirmed; cancellation admitted, query unavailable or response lost after the simulator commit | `confirmed` | `uncertain` on the original action id | Names the refund and says the schedule is unconfirmed |
| Cancellation confirmed; refund admitted and unknown | `uncertain` on `refund:{payment_id}` | `confirmed` | Names the schedule and says the refund is unconfirmed |
| Refund confirmed; cancellation blocked before admission, proposal still approved | `confirmed` | `not_dispatched` | Names the refund and says that cancellation attempt was not sent because the read was unavailable |
| Same approved cancellation enqueued again after a read block | `confirmed` | Newest version is `in_flight` only when input, grant, and payload are unchanged and nothing is admitted; the blocked version remains history | None while the new attempt is in flight |
| New input while that cancellation is blocked before admission | The confirmed refund stays | Old attempt is `not_executed`; the old approval does not execute the new input | The unsent draft is superseded |
| Both confirmed | `confirmed` | `confirmed` | Both receipts, bound to those two outcome ids |
| Both ineligible | `not_executed` | `not_executed` | None; ticket stays `open` |
| New input during pre-admission | That unsent attempt becomes `not_executed` | The sibling follows its own stage | No new dispatch for an admitted sibling |
| New input after admission | Admitted half stays `uncertain` or `confirmed` | Same fence if that half is admitted | No fresh proposal for an admitted action id |
| Two workers finish in either order | Each ledger changes once | Each ledger changes once | One current draft bound to both final refs |
| Input commits before send | Unsent draft superseded | Current halves follow the new revision | Send of the old draft is stale |
| Send commits before input | Sent receipt unchanged | Later input creates a new current draft | Repeat send returns the same receipt |
| Reject the current draft, then reduce again | Unchanged | Unchanged | A new unsent revision for the same binding; the rejected draft is not current |
| Reject a historical draft | Unchanged | Unchanged | Current ticket, run, and draft unchanged |
| Requeue one `delivery_uncertain` job | Only that job returns to query-only | Sibling unchanged | No second dispatch |
| Incident or compensation phrase | No combined rows | No combined rows | ADR 0010 |
| Ordinary single-purpose refund or cancellation | Unchanged | Unchanged | Unchanged single-action draft |

B1 must use the same two gateways, the same reducer, and the same incident
refusal. That is a competence check, not a comparison of agents. Do not start
the 240-case evaluation from this slice.
