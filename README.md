# ResolveAI — Agentic Customer Operations Platform

**English** · [简体中文](README.zh-CN.md)

A local workbench for synthetic SaaS billing tickets. It investigates a ticket,
retrieves policy evidence, proposes a refund, and waits for a separate approver
before executing against a business simulator and drafting a reply.

Customers, invoices, payments, and refunds are synthetic. This is not a production
payment or customer messaging service. The default process uses a fake model and a
zero budget. A live model is a separate process and stays off unless the owner
turns it on.

The current build covers duplicate-payment refunds for one tenant. Subscriptions,
a second tenant, and recovery after a process crash are not included.

See [product scope](docs/PRODUCT_SCOPE.md), [architecture](docs/ARCHITECTURE.md),
and [decisions](docs/adr/README.md).

## Development setup

Use Python 3.12, uv, Node 24 and pnpm 11.25.0. Python dependencies are pinned in
`uv.lock` and the workbench in `pnpm-lock.yaml`. Dependency resolution is not an
application test.

```sh
make env
make check-setup
make db-up
make migrate
make seed
```

Start the three local processes in separate terminals:

```sh
make simulator
make api
make web
```

Open http://127.0.0.1:5173. The sign-in screen lists the synthetic support and approver
accounts. After sign-in, the header has a language menu for English and Simplified
Chinese. New investigation notes and reply drafts use the language selected when they
are created. Customer ticket text stays in the language of the fixture. The API is
http://127.0.0.1:8000 and the simulator is http://127.0.0.1:8001. Both bind to
localhost. The workbench proxies `/api` to the API.

The database is dedicated to ResolveAI and bound to localhost port 55432. Development
credentials in compose.yaml and `.env.example` are synthetic placeholders. Migrations
create the `app` and `simulator` schemas, the vector extension, and restricted runtime
roles. The initializer role is used for migrations and seeding only.

Docker Engine must be running for `make db-up`. `make check-setup` validates
configuration and imports without starting containers. If using a different host,
inspect tool versions first; no existing PostgreSQL instance should be reset.

Copy `.env.example` to `.env` to override the documented defaults. Provider defaults
are fake and the live-model budget is zero. A non-fake provider, a model name, an API
key, or a non-zero budget prevents the default startup. Do not insert real API keys
into tracked files. `OPENAI_API_KEY` may live only in the untracked `.env`. The
default API ignores it.

```sh
make test
corepack pnpm install --frozen-lockfile
corepack pnpm --dir apps/web test:e2e
```

`make test` runs Ruff and pytest. Playwright drives one approved refund and one rejected
proposal, and creates `resolveai_pytest` itself when that database is missing. Both suites
use that local database. Playwright serves the API on port 18080, the simulator on port
18081, and the workbench on port 15173. Run one suite at a time. `make seed` loads
`resolveai_demo`. Run `make migrate` before seeding so the policy tables exist.

A refund amount comes from the policy and the payment record. Customer text does not
set it, and support cannot approve their own proposal. If a refund response is lost,
the original action id is queried rather than submitted again. Replies are simulated
receipts. Sending one does not close the ticket.

Licensing has not been selected. Public visibility alone does not grant an open-source
license.
