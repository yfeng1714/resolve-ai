.PHONY: env check-setup db-up db-down migrate seed api simulator web test check

export PYTHONPATH := $(CURDIR)/src
UV := UV_CACHE_DIR=$(CURDIR)/.uv-cache uv
PNPM := corepack pnpm

env:
	UV_CACHE_DIR=$(CURDIR)/.uv-cache uv sync --locked

check-setup:
	UV_CACHE_DIR=$(CURDIR)/.uv-cache uv lock --check
	.venv/bin/python -c "import fastapi, langgraph, pydantic, sqlalchemy, psycopg, pgvector, httpx, uvicorn, alembic; print('Python dependency imports OK')"
	docker compose config --quiet

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

migrate:
	$(UV) run alembic upgrade head

seed:
	$(UV) run python -m resolveai.seed

api:
	$(UV) run uvicorn resolveai.api.app:app --host 127.0.0.1 --port 8000 --app-dir src

simulator:
	$(UV) run uvicorn resolveai.simulator.app:app --host 127.0.0.1 --port 8001 --app-dir src

web:
	$(PNPM) --dir apps/web dev

test:
	$(UV) run ruff check src tests migrations
	$(UV) run pytest

check: check-setup test
