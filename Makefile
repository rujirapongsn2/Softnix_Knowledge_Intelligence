.PHONY: up test lint
up:
	docker compose up --build
test:
	cd apps/api && pytest
lint:
	cd apps/api && ruff check .

