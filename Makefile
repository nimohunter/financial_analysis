.PHONY: up down logs shell-backend shell-db migrate smoke

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f backend

shell-backend:
	docker compose exec backend bash

shell-db:
	docker compose exec postgres psql -U finagent -d finagent

migrate:
	docker compose exec backend alembic upgrade head

smoke:
	@echo "Smoke test: ingest AAPL since 2024-01-01"
	docker compose exec backend python -m app.ingest --tickers AAPL --since 2024-01-01
	@echo "Smoke test complete."
