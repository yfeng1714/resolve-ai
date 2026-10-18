# ADR 0001: Stack and staged runtime

Status: Accepted for M1. Date: 2026-10-12.

Problem: start from an empty repository without making infrastructure a prerequisite
for demonstrating an agent.

Alternatives: full TypeScript backend; Next.js plus Python; React/Vite plus FastAPI.

Choice: React/TypeScript/Vite and pnpm; Python 3.12/uv, FastAPI/Pydantic, SQLAlchemy/
Alembic, bounded LangGraph and PostgreSQL/pgvector. One backend dependency lock;
separate simulator HTTP service. Core uses API-driven execution; Portfolio adds
durable worker/checkpoint. No Redis, Kubernetes or multiple backend frameworks.

Cost: two languages and process boundaries; frontend dependencies are selected and
locked during M1. Current M0 installs Python dependencies but creates no app code.

Reconsider: an existing reusable component appears, measured limits justify a queue,
or dependency compatibility breaks. Validate against lockfiles and actual checks.

Sources checked: [Vite](https://vite.dev/guide/),
[LangGraph install](https://docs.langchain.com/oss/python/langgraph/install),
[uv environment settings](https://docs.astral.sh/uv/reference/environment/).
