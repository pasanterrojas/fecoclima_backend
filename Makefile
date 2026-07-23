.PHONY: env up down build logs ps migrate seed test lint format shell clean

env:
	@test -f .env || cp .env.example .env

up:
	docker compose up -d --build

down:
	docker compose down

build:
	docker compose build --pull

logs:
	docker compose logs -f api worker scheduler

ps:
	docker compose ps

migrate:
	docker compose run --rm migrate

seed:
	docker compose run --rm api python -m app.scripts.init_db

test:
	python -m pytest -q

lint:
	ruff check .

format:
	ruff format .

shell:
	docker compose exec api sh

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
