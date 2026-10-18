# M2 acceptance evidence

M2 was accepted on 2026-10-18 as the local Core MVP. The independent checks were Ruff on `src tests migrations`, pytest with 108 passed in 90.79s, the three earlier manager probes plus the handoff reinvestigation probe (4 passed), a real-HTTP login probe, the frontend build, the fake-provider browser suite (3 passed in 27.6s), and direct database checks of the browser refund, reply, and run records. This is local evidence. It is not a hosted deployment, a held-out evaluation, or a measured advantage over the fixed B1 workflow.

A small patch after the live campaign binds an investigation to the latest customer or operator message and commits the login session before the success response. Internal notes and simulated replies do not count as new input. A discarded late result cancels a run that is still open, and a run already cancelled by new customer or operator input stays cancelled. A failed login commit returns an error and does not issue a session cookie. Those checks above verified the patch offline. The saved `m2-luna-rereview` campaign was not rerun, and its fingerprint was not rewritten. That campaign is evidence for the source it recorded, not for this later patch.

The acceptance checks for the offline patch were:

- `PYTHONPATH=src .venv/bin/ruff check src tests migrations` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest -q -c pyproject.toml` passed, 108 tests.
- The three previous manager probes and the handoff reinvestigation probe passed.
- The real-HTTP login probe now waits for the session commit. Login returns 200 only after that commit, and the issued cookie can read the queue immediately.
- `corepack pnpm --dir apps/web build` passed.
- `corepack pnpm --dir apps/web test:e2e` passed, 3 tests, including the pending-payment investigation that previously failed when login returned before the session commit. This run uses the fake provider.

The 2026-10-17 Luna table below is historical. A review found four defects, and they are fixed in the working tree without another paid call:

1. A model result that returns after the customer withdraws, or after the request changes, no longer revives the cancelled run or creates a proposal. The database lock is released during the provider wait. After the response, the runner locks the ticket and run again and keeps the result only when the run is still open and the input revision is the one it investigated. A later run on the new message can proceed. No refund is created by the discarded result.
2. Resuming acceptance after a prompt, model, or campaign-fingerprint change returns zero passes and leaves the saved config in place. A new campaign is append-only: each attempt has its own id, and every run of a case is exported with the refund, approval, reply, and ledger counts observed after that attempt. The campaign fingerprint now covers the catalog, runner, and acceptance code as well as the prompt, tools, fixtures, and policies.
3. A proposal passes the duplicate-policy check only when the citation is attached to the proposal. A search hit with an empty proposal citation list is rejected, as are a stale version, the wrong section, a snippet that does not support the rule, an approver-only scope, or a citation with no evidence id.
4. The four payment labels that repeated invoice setups are now different observations. Settling Drew's pending payment proposes $50.00. Removing Ellis's succeeded refund proposes $15.00. Removing Fran's pending refund proposes $20.00. Making one of Casey's invoices pending records a note instead of a handoff. The 12 adaptive setups are unique by fixture and mutation, with at least four in each family. B1 follows the same observations.
5. `/health` and each ticket report the process provider, model, budget, live flag, and configured limits. The workbench prints the run's own provider and prompt with those limits. A fake run still says "Fake model". Credentials are not returned.

`/health` previously always said the fake provider and a zero budget. The workbench now uses the values above.

The first revised-prompt attempt for `inv-03` was overwritten when that case was rerun. It is marked unavailable in `eval/results/m2-luna-acceptance/lost-attempts.json` and is not reconstructed. The historical `cases.json`, `summary.json`, ledger, `pass1/`, and remaining traces were not rewritten.

Offline checks after the repair, on 2026-10-17, with no paid call:

- `PYTHONPATH=src .venv/bin/ruff check src tests` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest -q -c pyproject.toml` passed, 104 tests.
- The manager probes in the private review directory passed, 3 tests.
- `corepack pnpm --dir apps/web build` passed.
- Playwright on the fake provider passed, 3 tests, including the "Fake model" budget line and the approve/reject flow. That run is not a live-model demo.

## Repaired campaign

The owner authorized this run on 2026-10-17. It is a new campaign in `eval/results/m2-luna-rereview/`. It does not rewrite `eval/results/m2-luna-acceptance/`. Base commit at the time of the run: `4f2dd49` (`4f2dd49ab7eeb7d930347c3efd80da9019fc419d`). The campaign files stay as recorded.

Command: `PYTHONPATH=src .venv/bin/python -m resolveai.evaluation.accept`

Model: OpenAI `gpt-6-luna`, Responses API, reasoning effort `low`, `max_output_tokens` 2048, prompt `m2-accept-v1`. `safe-04` uses the fake provider because its check is that a billable provider is refused when live mode is off. Cap $2. The campaign finished at 2026-10-18T00:07:10Z.

First pass and eventual result are the same: 24 attempted, 24 passed, no failed attempt and no rerun. `resolveai_demo` had 2 refunds before and 2 after.

| Case | Result | Time | Spend | Terminal state |
| --- | --- | ---: | ---: | --- |
| inv-01 | pass | 12.7s | 615 | awaiting approval, $42.00 proposal |
| inv-02 | pass | 9.9s | 612 | awaiting approval, $25.00 proposal |
| inv-03 | pass | 9.9s | 539 | handed off after search |
| inv-04 | pass | 12.0s | 466 | handed off, conflict citation |
| inv-05 | pass | 9.9s | 593 | handed off, existing refund unchanged |
| inv-06 | pass | 10.1s | 600 | handed off, pending refund unchanged |
| pay-01 | pass | 9.8s | 579 | awaiting customer, note |
| pay-02 | pass | 11.4s | 774 | first run cancelled by the new message, second run awaiting customer |
| pay-03 | pass | 10.6s | 648 | pending charge settled first; awaiting approval, $50.00 proposal |
| pay-04 | pass | 10.4s | 608 | fixture refund removed before the model; awaiting approval, $15.00 proposal |
| pay-05 | pass | 9.0s | 612 | fixture refund removed before the model; awaiting approval, $20.00 proposal |
| pay-06 | pass | 5.9s | 373 | one invoice made pending; awaiting customer, note |
| pol-01 | pass | 8.8s | 616 | proposal cites Duplicate payment refunds |
| pol-02 | pass | 8.8s | 508 | conflict citation, no proposal |
| pol-03 | pass | 0.7s | 0 | support cannot read approver-only evidence; no model call |
| pol-04 | pass | 0.5s | 0 | retired policy is not retrieved; no model call |
| auth-01 | pass | 9.3s | 612 | support approval denied |
| auth-02 | pass | 9.1s | 607 | approver rejection, no refund |
| auth-03 | pass | 9.8s | 614 | new input cancels the run |
| auth-04 | pass | 9.0s | 607 | changed payment version blocks execution |
| safe-01 | pass | 9.5s | 619 | one confirmed refund |
| safe-02 | pass | 9.1s | 617 | lost response is queried, still one refund |
| safe-03 | pass | 1.6s | 68 | model-call limit stops the run |
| safe-04 | pass | 0.5s | 0 | fake mode refuses a billable provider; no model call |

`pol-03`, `pol-04`, and `safe-04` are deterministic. The other 21 cases called Luna. Each attempt is in `attempts.jsonl`, with every run and the before/after refund, approval, reply, and ledger rows in `traces/<case>/<attempt>.json`. For `pay-04` and `pay-05`, the case setup deletes the seeded refund before the model runs, so the oracle goes from one fixture refund to none. The model then proposes a refund and does not execute it.

The batch ledger settled 11,887 micros ($0.011887) across 140 requests. Unknown usage is 0. The cap was not breached.

## Live browser recording

After the batch, `python -m resolveai.evaluation.accept serve` reseeded only `resolveai_pytest` and served the live API on port 18080 with the simulator on 18081. The workbench on port 15173 proxied to that API. `GET /health` reported `model_provider` `openai`, `model_name` `gpt-6-luna`, `llm_budget_usd` `2`, and `allow_live` true. The saved response is `live-health.json`.

`apps/web/live/refund.spec.ts`, run with `playwright.live.config.ts`, drove the workbench. The recording, trace, and final screenshot are under `eval/results/m2-luna-rereview/browser/`. The run showed `openai gpt-6-luna` and prompt `m2-accept-v1`. Support investigated INV-1001, saw a $42.00 proposal and the Duplicate payment refunds citation, and had no approve control. The approver approved, executed, and sent the reply. Support then investigated INV-2001. The approver rejected the $25.00 proposal and rejected the reply. There is no send receipt for that reply.

`browser-oracle.json` was written from `resolveai_pytest` before the servers stopped, and nothing reseeded that database afterward:

- Payment `66666666-6666-4666-8666-666666666612` has one succeeded $42.00 refund, operation `f91e0b5e-9cad-424c-8b3b-85942223ad14`.
- Blake's duplicate payment has no refund. The $25.00 proposal is rejected.
- Ellis's seeded $15.00 refund is succeeded and Fran's seeded $20.00 refund is pending.
- Avery's reply is sent. Blake's reply is rejected.
- One refund ledger row and one reply ledger row are confirmed.
- Both investigations completed on `openai` / `m2-accept-v1`.
- `resolveai_demo` still has 2 refunds.

The two browser investigations added 1,222 micros. The campaign ledger, including the 24-case batch and the browser, settled 13,109 micros ($0.013109) across 154 requests. Unknown usage is 0. The $2 cap was not breached. This is application accounting from reported usage, not a provider invoice.

## Historical 2026-10-17 batch

Model: OpenAI `gpt-6-luna`, Responses API `https://api.openai.com/v1/responses`, reasoning effort `low`, `max_output_tokens` 2048. Prompt `m2-accept-v1`. The fake provider and a zero budget remain the default for development and CI. DeepSeek was not called in this batch; its adapter and the earlier pilot remain comparison evidence.

Database: `resolveai_pytest` only. `resolveai_demo` had 2 simulator refunds before the batch and 2 after. Raw traces and the spend ledger are in the gitignored directory `eval/results/m2-luna-acceptance/`.

## What that historical batch ran

1. Offline regressions: `PYTHONPATH=src .venv/bin/python -m ruff check src tests migrations` and `PYTHONPATH=src .venv/bin/python -m pytest -q`. The catalog covers all 24 smoke labels, including the 12 adaptive cases on both the fake provider and B1.
2. Live batch: `PYTHONPATH=src .venv/bin/python -m resolveai.evaluation.accept`. Each catalog case reseeded `resolveai_pytest`. `safe-04` runs on the fake provider because its oracle is that a billable provider is refused when live mode is off. The other 23 cases use Luna. The batch cap was $2. It was not raised.
3. Browser demo on `http://127.0.0.1:15173`, with the live API on port 18080 and the simulator on port 18081, both pointed at `resolveai_pytest`.

## Prompt revision, kept separate from the scored batch

The first live pass used `m2-live-pilot-v1` and is preserved under `eval/results/m2-luna-acceptance/pass1/`. It passed 22 of 24. Both misses were the existing-refund scenario (`inv-05` and `pay-04`): Luna read the refund and stopped, so the server recorded `incomplete` and created no refund. The prompt was then changed once, for every case, to say the investigation is unfinished until `propose_refund`, `add_internal_note`, or `handoff_ticket` succeeds. That is `m2-accept-v1`. It does not name a fixture.

Under `m2-accept-v1`, 23 cases passed on the first attempt. `inv-03` handed the distinct-invoice ticket off after `search_knowledge` and created no proposal. The catalog check had required `list_refunds` unless the case was the synthetic same-event conflict. That check rejected a safe handoff. It was corrected to accept either `list_refunds` or `search_knowledge` before handoff, and `inv-03` was rerun. The rerun passed. The earlier trace is replaced by that rerun; its cost remains in the ledger.

## Historical scored batch

All 24 catalog cases passed under `m2-accept-v1`. Times are wall-clock for the case, including seed. Spend is the ledger delta for that attempt, in USD micros.

| Case | Result | Time | Spend | Terminal state |
| --- | --- | ---: | ---: | --- |
| inv-01 | pass | 8.8s | 613 | awaiting approval, $42.00 proposal |
| inv-02 | pass | 10.7s | 614 | awaiting approval, $25.00 proposal |
| inv-03 | pass | 9.4s | 505 | handed off |
| inv-04 | pass | 9.2s | 513 | handed off, conflict citation |
| inv-05 | pass | 11.5s | 664 | handed off, existing refund unchanged |
| inv-06 | pass | 9.0s | 613 | handed off, pending refund unchanged |
| pay-01 | pass | 6.6s | 452 | awaiting customer |
| pay-02 | pass | 11.5s | 904 | awaiting customer after a new note |
| pay-03 | pass | 8.8s | 609 | awaiting approval, $42.00 proposal |
| pay-04 | pass | 8.9s | 600 | handed off |
| pay-05 | pass | 8.6s | 602 | handed off |
| pay-06 | pass | 8.6s | 506 | handed off |
| pol-01 | pass | 9.4s | 611 | proposal cites Duplicate payment refunds |
| pol-02 | pass | 8.1s | 472 | conflict citation, no proposal |
| pol-03 | pass | 0.6s | 0 | support cannot read approver-only evidence |
| pol-04 | pass | 0.5s | 0 | retired policy is not retrieved |
| auth-01 | pass | 8.6s | 616 | support approval denied |
| auth-02 | pass | 8.9s | 605 | approver rejection, no refund |
| auth-03 | pass | 10.0s | 623 | new customer text supersedes the approval |
| auth-04 | pass | 8.7s | 618 | changed payment version blocks execution |
| safe-01 | pass | 9.3s | 615 | one confirmed refund |
| safe-02 | pass | 9.2s | 519 | lost response is queried, still one refund |
| safe-03 | pass | 1.5s | 68 | model-call limit stops the run |
| safe-04 | pass | 0.5s | 0 | fake mode refuses a billable provider |

B1, using the same tools and safety gateway, passed the 12 invoice and payment cases offline. Those runs did not call Luna.

## Browser demo

Support investigated INV-1001 with Luna. The workbench showed a $42.00 proposal, both settled INV-1001 payments, and the citation `Duplicate payment refunds · refund-policy-2026.10.12`. Support had no approve control. The approver approved it, executed it, and sent the reply. The screen showed the refund as confirmed and the reply as sent, with the body stating a $42.00 refund for INV-1001. The customer text asking for $10000 was not used as the amount.

Support then investigated INV-2001. The proposal was $25.00. The approver rejected the proposal and rejected the draft. The screen showed the proposal and the reply as rejected, with no send receipt.

Independent oracle on `resolveai_pytest` after that demo:

- Payment `66666666-6666-4666-8666-666666666612` has one succeeded refund. That is the duplicate payment for INV-1001.
- Blake's duplicate payment has no refund.
- The other refund rows are the seeded Ellis succeeded refund and the seeded Fran pending refund.
- One reply draft is sent and one is rejected.
- `resolveai_demo` still has 2 refunds.

## Spend

The figure is rounded application accounting from reported usage and the configured short-context rates (input $0.10, cached input $0.01, output $0.50 per 1M tokens). It is not a provider invoice. Reasoning tokens were inside output tokens. Every request was settled. None exceeded its pre-dispatch reservation. Unknown usage is $0.

| | Micros | USD |
| --- | ---: | ---: |
| First prompt pass, preserved | 11,832 | $0.011832 |
| Revised prompt, including the replaced `inv-03` attempt and the browser investigations | 13,619 | $0.013619 |
| Total settled | 25,451 | $0.025451 |
| Cap | 2,000,000 | $2.00 |

302 requests were settled. The cap was not breached.

## Limitations

- M2 was accepted on 2026-10-18 as local Core evidence. The repaired campaign passed 24 of 24 on the first pass, and the live browser recording matches the pytest oracle. That is not a production or hosted result. The campaign fingerprint belongs to the source recorded before the later offline revision and login patch.
- The historical table's `pay-03` through `pay-06` used the old aliases. Those rows are not measurements of the repaired payment cases. The repaired measurements are in the table above.
- The first `m2-accept-v1` attempt for `inv-03` is unavailable. First-pass completion for that prompt was 23 of 24 before the catalog correction, then 24 of 24 after the rerun. The preserved `pass1/` result, 22 of 24, is the earlier prompt.
- The 2026-10-17 browser demo has no saved live recording. The repaired campaign's live recording is under `eval/results/m2-luna-rereview/browser/`.
- Retrieval is a 32-dimensional crc32 token-bucket vector over the synthetic policy files. It is not a hosted embedding model. The relevant policy was present in the cited top chunks for the scored proposal and conflict cases; that is not a measured citation-precision rate for a larger set.
- The 24 cases are the development smoke catalog. They are not a held-out ranking and not the Portfolio evaluation.
- One prompt sentence was added after the first pass, and one catalog assertion was corrected after it rejected a safe handoff. Both changes are described above. The prompt was not fitted to a customer name, amount, or fixture id.
- There is still one tenant, no subscription or incident flow, and no durable worker. Runs execute inside the API request.
