# Development entry points. Run everything from this directory.
.PHONY: help install up down logs api worker web dev test lint format typecheck check images clean ragas-env

help:
	@echo "make install    install python + node dependencies"
	@echo "make up         start postgres + redis (add: --profile storage for minio)"
	@echo "make down       stop infrastructure"
	@echo "make api        run FastAPI with hot reload on :8100"
	@echo "make worker     run the scheduler (scheduled runs, stranded-run sweep)"
	@echo "make web        run Next.js with hot reload on :3100"
	@echo "make test       pytest"
	@echo "make check      lint + format check + typecheck + test   (what CI runs)"
	@echo "make ragas-env  build .venv-ragas, the isolated env the Ragas eval arm runs in"
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

# The scheduler. A third process, alongside the API and the web app: it is what makes
# an automation setting do something. Without it `automation_settings.next_run_at` is a
# value nothing reads. No queue broker to run -- the due list is a database scan; see
# `backend/app/worker/scheduler.py` for why there is no arq.
worker:
	uv run python -m backend.app.worker

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

# The Ragas eval arm runs OUT-OF-PROCESS, in its own virtualenv, and it has to: `ragas`
# depends on `instructor`, which caps `openai<3.0.0`, while this project pins
# `openai>=3.2.0` deliberately (v3 is built on httpx2). See ARCHITECTURE.md section 14.
#
# Idempotent: safe to re-run, and re-running is how you pick up a change to
# evals/ragas-requirements.txt. Gitignored -- a second virtualenv is a build artifact.
ragas-env:
	uv venv .venv-ragas --python 3.13
	VIRTUAL_ENV=.venv-ragas uv pip install -r evals/ragas-requirements.txt
	@.venv-ragas/bin/python -c "import ragas; print(f'ragas {ragas.__version__} ready in .venv-ragas')"
	@uv run python -m evals.ragas_arm || true

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache frontend/.next
