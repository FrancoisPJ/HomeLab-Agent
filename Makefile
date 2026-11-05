# homelab-agent — raccourcis. `make help` pour la liste.
SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose
PROFILE ?= full
CONFIG ?= config.yaml
PRESET ?=
RUN := $(UV) run

.DEFAULT_GOAL := help
.PHONY: help setup init lint format typecheck test cov check schema schema-check engines models \
        up down restart logs ps dev dev-down dev-monitoring e2e bench build gitleaks

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

setup: ## Install Python deps (uv) and git hooks
	$(UV) sync
	$(RUN) pre-commit install || true

init: ## Create .env, config.yaml, data/, models/ (PRESET=cpu-only for a host without GPU)
	@test -f .env || (cp .env.example .env && python3 scripts/init_env.py .env && echo "created .env — edit it")
	@test -f $(CONFIG) || (cp config$(if $(PRESET),.$(PRESET)).example.yaml $(CONFIG) && echo "created $(CONFIG) from the $(or $(PRESET),default) preset — edit it")
	@mkdir -p data models engines

lint: ## Ruff lint + format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

format: ## Auto-format
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

typecheck: ## mypy --strict
	$(RUN) mypy

test: ## Unit + integration tests (fake backends)
	$(RUN) pytest

cov: ## Tests with coverage report
	$(RUN) pytest --cov --cov-report=term-missing

schema: ## Regenerate config.schema.json
	$(RUN) homelab-agent schema -o config.schema.json

schema-check: ## Fail if config.schema.json is stale
	@$(RUN) homelab-agent schema | diff -u config.schema.json - && echo "schema up to date"

check: ## Validate config.yaml
	$(RUN) homelab-agent -c $(CONFIG) check-config

engines: ## Render llama-swap configs from models: in config.yaml
	$(RUN) homelab-agent -c $(CONFIG) render-engines -o engines

models: ## Download the GGUF files listed in config.yaml (checks sha256)
	CONFIG=$(CONFIG) ./scripts/download-models.sh

# PROFILE may list several profiles: make up PROFILE=full,voice
export COMPOSE_PROFILES = $(PROFILE)

build: ## Build the Docker images of the profile
	$(COMPOSE) build

up: init engines ## Start the stack (PROFILE=full|cpu-only|no-gpu, add ",voice" for voice notes)
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

restart: engines ## Restart (after a config/model change)
	$(COMPOSE) up -d --force-recreate

logs: ## Follow logs
	$(COMPOSE) logs -f --tail=100

ps: ## Containers status
	$(COMPOSE) ps

dev: ## Dev stack with fake services + hot reload
	COMPOSE_PROFILES=dev $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml up --build

dev-down:
	COMPOSE_PROFILES=dev,monitoring $(COMPOSE) down -v

dev-monitoring: ## Dev stack + Prometheus + Grafana (http://127.0.0.1:3000, admin / change-me-grafana)
	COMPOSE_PROFILES=dev,monitoring $(COMPOSE) up -d --build --wait
	@echo "panel: http://127.0.0.1:8099 (dev-password-123) · grafana: http://127.0.0.1:3000"

e2e: ## End-to-end scenario against the dev stack
	COMPOSE_PROFILES=dev $(COMPOSE) up -d --build --wait
	HLA_E2E=1 $(RUN) pytest -m e2e tests/e2e -v; status=$$?; \
	  COMPOSE_PROFILES=dev $(COMPOSE) logs agent-dev | tail -50; \
	  COMPOSE_PROFILES=dev $(COMPOSE) down -v; exit $$status

bench: ## Benchmark an endpoint: make bench URL=http://localhost:18080/v1 MODEL=fast
	$(RUN) python scripts/bench.py --url $(or $(URL),http://127.0.0.1:18080/v1) --model $(or $(MODEL),fast)

gitleaks: ## Scan the whole git history for secrets (needs docker)
	docker run --rm -v "$(CURDIR):/repo" zricethezav/gitleaks:v8.29.1 git /repo --redact -v
