.PHONY: build up down restart logs lint dev clean purge-uploads

build:
	docker compose up --build -d

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose down
	docker compose up --build -d

logs:
	docker compose logs -f

lint:
	uv run ruff check backend/

dev:
	uv run uvicorn backend.main:app --reload --port 8000

purge-uploads:
	rm -rf uploads/*
	docker volume rm editpdf_uploads 2>/dev/null || true

clean:
	docker compose down -v
	docker image prune -f
