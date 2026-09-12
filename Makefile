# Convenience targets. Everything here is also documented in README.md.
.PHONY: up down logs models test test-unit test-integration dev-api dev-web ingest status clean

up:            ## Build and start everything (UI :3000, API :8000)
	docker compose up --build

down:          ## Stop containers (keeps data volumes)
	docker compose down

logs:          ## Tail API logs
	docker compose logs -f api

models:        ## Pull the local models used by the demo
	ollama pull qwen2.5:7b
	ollama pull nomic-embed-text

test:          ## Run the full backend test suite (needs Postgres; see README)
	cd backend && python -m pytest -q

test-unit:     ## Unit tests only — no database required
	cd backend && python -m pytest -q tests/test_units.py tests/test_ingest.py

test-integration: ## Integration tests inside the compose network
	docker compose run --rm -e TEST_DATABASE_URL=postgresql+asyncpg://lenny:lenny@db:5432/lenny_test api python -m pytest -q

dev-api:       ## Run the API with autoreload (no Docker)
	cd backend && uvicorn app.main:app --reload --port 8000

dev-web:       ## Run the UI with Vite (no Docker)
	cd frontend && npm run dev

ingest:        ## Trigger an incremental re-ingest of the transcripts
	curl -s -X POST localhost:8000/api/admin/ingest && echo

status:        ## Readiness: DB, providers, index freshness
	curl -s localhost:8000/health/ready | python -m json.tool

clean:         ## Stop and delete volumes (database + cloned transcripts)
	docker compose down -v
