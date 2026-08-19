# Development entry points. Run everything from this directory.
.PHONY: help install up down logs api web dev test lint format typecheck check images clean

help:
	@echo "make install    install python + node dependencies"
	@echo "make up         start postgres + redis (add: --profile storage for minio)"
	@echo "make down       stop infrastructure"
	@echo "make api        run FastAPI with hot reload on :8100"
	@echo "make web        run Next.js with hot reload on :3100"
	@echo "make test       pytest"
	@echo "make check      lint + format check + typecheck + test   (what CI runs)"
	@echo "make images     build both docker images locally"

install:
	uv sync
	cd frontend && pnpm install

up:
	docker compose up -d postgres redis
	@echo "postgres :5435   redis :6381"

down:
	docker compose down

logs:
	docker compose logs -f

api:
	uv run uvicorn backend.app.asgi:app --reload --port 8100

web:
	cd frontend && pnpm dev

# Two processes are needed; run `make api` and `make web` in separate terminals.
dev:
	@echo "Run in two terminals:  make api   |   make web"

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy backend
	cd frontend && pnpm typecheck

check: lint typecheck test
	uv run ruff format --check .
	@echo "all gates green"

images:
	docker build -f backend/Dockerfile -t sma-api:local .
	docker build -f frontend/Dockerfile -t sma-web:local frontend

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache frontend/.next
