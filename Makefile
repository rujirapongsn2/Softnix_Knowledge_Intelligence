.PHONY: up test test-postgres lint
up:
	docker compose up --build
test:
	cd apps/api && pytest
# Queue/pgvector tests against a throwaway PostgreSQL (database name must end in _test).
test-postgres:
	docker compose -f docker-compose.pgvector-test.yml run --rm api-pgvector-test; \
	status=$$?; docker compose -f docker-compose.pgvector-test.yml down -v; exit $$status
lint:
	cd apps/api && ruff check .

