.PHONY: help install test test-unit lint docs clean stack stack-down stack-env

PY ?= python3
VENV ?= .venv

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create a venv and install the CLI (editable, with test deps)
	$(PY) -m venv $(VENV)
	. $(VENV)/bin/activate && pip install -q -e '.[test]'

test: ## Run the full suite against the throwaway stack (boots it if needed)
	$(MAKE) stack
	. $(VENV)/bin/activate && eval "$$(./scripts/bootstrap_test_stack.sh --export)" && python -m pytest

stack: ## Boot Grafana + Loki + Prometheus for testing
	docker compose up -d --wait

stack-down: ## Tear the test stack down, volumes and all
	docker compose down -v

stack-env: ## Print the env for the running stack: eval "$$(make -s stack-env)"
	@./scripts/bootstrap_test_stack.sh --export 2>/dev/null

test-unit: ## Run the hermetic tests (no server, no Docker, no token, ~2s)
	# The MARKER is the source of truth, never a file list. The sibling
	# OpenProject CLI ran `pytest tests/test_unit.py` here -- 30 of its 144
	# hermetic tests -- while its help claimed to run them all, and silently
	# missed every file added afterwards.
	. $(VENV)/bin/activate && python -m pytest -m "not integration"

lint: ## Lint with ruff (not a declared dep: pip install ruff, or use uvx/pipx)
	@command -v ruff >/dev/null 2>&1 || { \
	  echo "ruff not found. Install it: pip install ruff   (or: uvx ruff check src tests scripts)"; \
	  exit 1; }
	ruff check src tests scripts

docs: ## Regenerate docs/COMMANDS.md from the live command tree
	. $(VENV)/bin/activate && python scripts/gen_docs.py

clean: ## Remove build artifacts and caches
	rm -rf dist build *.egg-info src/*.egg-info .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
