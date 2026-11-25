# ADR 0008: Two-tenant isolation on the refund path

Status: Accepted for implementation on 2026-10-26, including the claim-state
clarification. The two-tenant refund path was accepted locally on 2026-10-28.
The slice adds a second
synthetic tenant and PostgreSQL row-level security to the existing refund,
approval, recovery, and retrieval path. It does not add subscriptions,
incidents, hybrid retrieval, or a paid evaluation.

Problem: tenant boundaries are application `WHERE` filters and a few composite
foreign keys. `resolveai_api` can read every app row. The worker claim selects
jobs with no tenant predicate. A missed filter, a pooled connection, or a
guessed object id is not stopped by the database. The simulator has no tenant
column; it authorizes an account id. Login is by email alone, while email is
unique only inside one tenant.

Alternatives: UI-only tenant switching; trusting model or header tenant ids;
giving the API role `BYPASSRLS`; using the initializer or oracle role as the
worker; a global `UPDATE` policy on every job; inventing a retrieval cache so
it can be isolated.

Choice: derive authority from the server session and from stored job rows. Set
a transaction-local tenant on every new transaction and fail closed when it is
absent. Login and session lookup are fixed security-definer functions. Job
discovery is one claim function, not a global permission to update jobs. A
seeded account binding stops a visible ticket from naming another tenant's
simulator account. Do not add a cache. Do not change an admitted command
because membership changed.

## What exists today

App tables carry `tenant_id`. Recovery jobs already reference the ledger,
proposal, run, approval, and authorizer with tenant-aware foreign keys. Most
other relationships are single-column foreign keys. There is no row-level
security. `user_account` is unique on `(tenant_id, email)`. Login selects one
row by email and then that user's membership.

`resolveai_api` is not superuser and does not own the tables, but its grants
are broad DML. The worker uses that same role. Its claim reads every
`recovery_job`. The oracle role is for tests. The initializer owns the tables
and is not a runtime connection.

The simulator schema has customers, accounts, invoices, payments, refunds, and
operations. Requests need the shared bearer token and an account id that
matches the path. `Ticket.account_id` and `Ticket.customer_id` are bare
uuids. The deterministic refund rule is one process-local file,
`policies/refund_policy_v1.json`. Knowledge search is not cached. It filters
documents and chunks by `tenant_id` and by `access_scope` for the caller's
role.

## Login emails

Normalized email is `lower(btrim(email))`. This slice adds a unique index on
that expression. One email identifies one user and therefore one tenant.
Login stays email and password. It does not gain a tenant field. Two fixture
tenants must not share an email. A user is not a member of both tenants.

## Authentication functions

The table owner is `resolveai_demo`, the initializer. It is not used as a
runtime connection. Two functions are `SECURITY DEFINER`, owned by that role,
so their bodies can read identity tables even when forced row-level security
hides those rows from runtime roles. Each function sets
`search_path = pg_catalog, app`. Object names in the body are schema-qualified.
There is no dynamic SQL and no caller-chosen table or column name.

`PUBLIC` execution is revoked. `resolveai_api` and `resolveai_worker` cannot
execute them. Only `resolveai_auth` can.

`app.lookup_login(p_email text)` returns at most one row: `user_id`,
`tenant_id`, `password_hash`, `role`, `display_name`, and normalized `email`.
The application checks the password, including the existing dummy-hash path
when no row is returned. The function does not create a session and does not
return other users.

`app.lookup_session(p_token_hash text)` returns at most one row when the hash
matches and `expires_at > clock_timestamp()`: `session_id`, `user_id`,
`tenant_id`, `role`, `display_name`, `email`, `csrf_hash`, and `expires_at`.
An expired or unknown hash returns no row. The API then rejects the request.
The function does not delete the expired row.

`resolveai_auth` can execute only those two functions. It has no `SELECT`,
`INSERT`, `UPDATE`, or `DELETE` on app tables. Executing a function is not
permission to query `user_account` or `user_session` directly.

After a password match, the server opens a new `resolveai_api` transaction,
sets that user's tenant, and inserts `user_session` and the login audit row.
The session cookie and CSRF cookie are set only after that commit succeeds.
A failed insert is rolled back and no cookie is issued. Logout, in a later
API transaction with the same tenant, deletes that session row. Expired
sessions fail `lookup_session` and are not reused.

Every later request calls `lookup_session` with the auth role, then opens a
new API transaction and sets `app.tenant_id` from the returned session row.
The role is the membership stored for that user and tenant. The request body
does not supply either value.

## Tenant context

`SELECT set_config('app.tenant_id', :tenant, true)` is transaction-local.
Commit and rollback clear it. The helper runs at the start of every new
transaction, not only at request entry. That includes the session insert
after login, enqueue, grant cleanup that commits, ticket reload, and each
worker transaction after claim. A pooled connection is tested after commit
and after rollback: with no new setting it sees no business rows.

Policies use `current_setting('app.tenant_id', true)`. A missing or blank
setting matches no rows and rejects writes. `resolveai_api` and
`resolveai_worker` are not table owners, not superuser, and not
`BYPASSRLS`. `FORCE ROW LEVEL SECURITY` is on for the protected tables.
Each request and each claimed job uses a new ORM session. A tenant change
does not reuse an identity map from the previous tenant.

Protected tables are `membership`, `user_session`, `ticket`, `message`,
`run`, `proposal`, `approval`, `approval_revocation`, `action_ledger`,
`tool_invocation`, `audit_event`, `reply_draft`, `recovery_job`,
`knowledge_document`, `knowledge_chunk`, and `tenant_account`. Ordinary
reads of `user_account` are also tenant-scoped. The two lookup functions are
the only unscoped identity reads.

## Worker claim

`app.claim_recovery_job()` is `SECURITY DEFINER`, owned by `resolveai_demo`,
with `search_path = pg_catalog, app`. `PUBLIC`, `resolveai_api`, and
`resolveai_auth` cannot execute it. Only `resolveai_worker` can.

The function selects one due row: `queued` with `next_attempt_at` already
reached, or `leased` with `leased_until` already passed, ordered by
`next_attempt_at, created_at`. It locks that row with `FOR UPDATE SKIP
LOCKED`, sets `state` to `leased`, assigns a new `owner_token`, increments
`owner_generation`, sets `leased_until` 30 seconds ahead, and sets
`updated_at`. It returns `job_id`, `tenant_id`, `owner_token`, and
`owner_generation`. It does not change the tenant, command, approval,
authorizer, or business payload, and it updates no other table. A queued row
is no longer selectable as queued while that lease is live. That global
discovery is the explicit exception. It is not permission for the worker
role to update every job.

`resolveai_worker` has no direct `UPDATE` that matches another tenant. Its
ordinary policies allow changes only where `tenant_id` equals the current
transaction setting. After `claim_recovery_job` commits, the next transaction
sets `app.tenant_id` from the returned `tenant_id` and then runs admission or
settlement. Those writes still require the job id, owner token, generation,
and unexpired lease. Losing that predicate rolls the business transaction
back. The worker does not use the initializer or oracle URL.

`resolveai_api` cannot execute `claim_recovery_job`. With tenant A set, it
can requeue or read only A's jobs. With that setting it cannot update B's
ticket, ledger, grant, or job. A direct test calls the claim function as the
worker and may receive B's due job. That result is the allowed exception, not
a leak through the API path.

## Account binding

`app.tenant_account` stores `tenant_id`, `account_id`, and `customer_id`.
`account_id` is unique, so one simulator account has one tenant. The
initializer seeds it. Runtime roles receive `SELECT` only.

`resolveai_api` and `resolveai_worker` cannot `UPDATE` `ticket.account_id` or
`ticket.customer_id`. A trigger rejects those column changes from either role.
An insert of a ticket must name an account that `tenant_account` binds to
`NEW.tenant_id`. Before each simulator call on this path, the app checks that
the loaded ticket's account and customer are visible in `tenant_account`
under the current tenant setting. That check precedes investigation reads,
eligibility reads, the refund POST, and later receipt reads. The POST also
requires the command's account to be the ticket's account. A receipt read
does not submit another refund. Row-level security on the ticket is not that
check.

A negative test keeps the transaction tenant as A, tries to point A's ticket
or refund command at B's account or payment, and expects denial. B's refund
count stays unchanged. This is not a test of a process that forges its own
tenant setting. The setting is server state. The test checks the account
binding the application claims.

The simulator stays account-scoped. The shared bearer token remains a
development secret. Using that token outside the app is a local-demo limit,
not a successful cross-tenant API call.

## Other boundaries

The migration also adds tenant-aware foreign keys where both sides already
have `tenant_id` and the link is still a bare id: memberships and sessions to
users, messages to tickets, runs to tickets, proposals to tickets and runs,
approvals to proposals and actors, revocations to approvals and actors,
ledger rows to proposals and replies, replies to tickets and runs, tool
invocations to runs, audit events to their actor, ticket, and run when those
are present, and knowledge chunks to documents. Existing recovery-job links
stay. These keys do not replace row-level security.

The approval-history trigger rejects `UPDATE` and `DELETE` from both
`resolveai_api` and `resolveai_worker`. Both roles can still lock an approval
row. Changing the id, decision, deadline, or actor through either runtime
role fails. The initializer can still move a deadline in a test.

Tenant B has its own users, accounts, payments, and knowledge rows. The
refund rule file stays one version. Tenant B's different outcome comes from
its own payments and documents. Search keeps `tenant_id` and `access_scope`.
No retrieval cache is added.

Membership changes are out of scope. This slice does not move a user between
tenants and does not revoke an admitted command because membership might
change later. Admission, expiry, and query-only recovery stay as accepted.

## Evidence

One Alembic revision adds the email index, the lookup and claim functions,
the auth and worker roles, row-level security, `tenant_account`, and the
composite foreign keys. Seed data for tenant B is initializer-only, in the
demo and test databases. The original M2 campaign fixtures are not relabeled.

Routes stay the same. There is no tenant field on write bodies. Tests use
the fake provider and a zero budget and keep the M3.1 and M3.2 suites and
browser flows green. They also show:

- tenant A and tenant B each complete their own refund outcome, with
  independent simulator counts and no duplicate or cross-account refund
- guessed ids for tickets, evidence, approvals, execution, revocation,
  requeue, and replies are denied
- context is set again after login's commit, after enqueue, and after a
  worker claim; a new ORM session is used for the other tenant
- the same pooled connection after commit and after rollback, then absent
  and blank settings, sees no business rows
- direct cross-tenant `INSERT`, `UPDATE`, and `DELETE` by `resolveai_api`
  fail
- function owner, `SECURITY DEFINER`, pinned search path, and execute
  grants match this ADR; `PUBLIC`, the API role, and the worker role cannot
  run the login functions; the API role cannot run `claim_recovery_job`
- the worker, while set to tenant A, cannot change B's ticket or an
  unrelated job; a later claim may still discover B's due job
- with tenant A fixed, redirecting A's ticket to B's account is denied and
  B gains no refund
- session rows belong to the looked-up tenant; logout and expiry reject the
  cookie
- expiry and a new worker process still query only the admitted action
- knowledge search does not return the other tenant's documents

One browser test signs in to each tenant, sees only its own queue, and is
denied the other tenant's ticket. Database checks, not the page filter, are
the isolation evidence. No deployment and no paid model batch.

Implementation waits for this revision to be accepted.
